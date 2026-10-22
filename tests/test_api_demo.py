import asyncio
from io import BytesIO
import unittest
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from gsme_rag.api import routes
from gsme_rag.api.app import DEMO_PAGE, create_app
from gsme_rag.api.schemas import ChunkRequest, CompareRequest


async def _inline(callback, *args):
    return callback(*args)


class APIDemoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Direct route checks avoid depending on a local HTTP server.
        patcher = patch.object(asyncio, "to_thread", _inline)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_health_and_registered_routes(self) -> None:
        self.assertEqual((await routes.health())["status"], "ok")
        self.assertIsInstance((await routes.health())["ocr_available"], bool)
        paths = create_app().openapi()["paths"]
        self.assertIn("/v1/parse", paths)
        self.assertIn("/v1/chunk", paths)
        self.assertNotIn("/v1/compare", paths)
        self.assertIn("/v1/schedules/preview", paths)
        self.assertNotIn("Compare all strategies", DEMO_PAGE)
        self.assertIn("3. Exact schedule filters (structured path)", DEMO_PAGE)

    async def test_parse_returns_markdown_without_chunks(self) -> None:
        upload = UploadFile(
            file=BytesIO(b"Tech,Flavor\n28,HPC+ RF\n"),
            filename="shared_schedules.csv",
        )
        # UploadFile.read may delegate to a thread for ordinary BytesIO files.
        async def read(size: int = -1) -> bytes:
            return upload.file.read(size)

        upload.read = read
        result = await routes.parse_preview(upload, "auto")

        self.assertEqual(result.filename, "shared_schedules.csv")
        self.assertIn("| Tech | Flavor |", result.markdown)
        self.assertNotIn("chunks", result.model_dump())

    async def test_chunk_and_compare_accept_markdown_without_upload(self) -> None:
        markdown = "# General\n\n**01 Why?**\n\nBecause it improves accuracy."
        chunks = await routes.chunk_preview(
            ChunkRequest(markdown=markdown, source_file="faq.md")
        )
        report = await routes.compare_preview(
            CompareRequest(
                markdown=markdown,
                cases=[{"question": "Why accuracy?", "evidence": "Because it improves accuracy."}],
            )
        )

        self.assertEqual(chunks.chunk_count, 1)
        self.assertEqual(chunks.chunks[0].chunk_type, "question_answer")
        self.assertEqual(report["valid_case_count"], 1)
        self.assertEqual(len(report["strategies"]), 4)

    async def test_chunk_response_exposes_review_status_only_when_needed(self) -> None:
        markdown = (
            "<!-- page: 1 -->\n\n<!-- review-required: ocr -->\n\n"
            "OCR 0.127 mm\n\n<!-- review-end -->\n\n"
            "<!-- page: 2 -->\n\nVerified specification."
        )
        result = await routes.chunk_preview(ChunkRequest(markdown=markdown, source_file="manual.pdf"))
        self.assertTrue(result.chunks[0].model_dump(exclude_none=True)["review_required"])
        self.assertNotIn("review_required", result.chunks[1].model_dump(exclude_none=True))

    async def test_chunk_response_exposes_pdf_category_without_general_parent(self) -> None:
        markdown = (
            "## General\n\n**01 Who do I contact?**\n\nEmail support.\n\n"
            "## TapeOut Services\n\n**01 What is the minimum die area?**\n\n"
            "It depends on the technology."
        )
        result = await routes.chunk_preview(
            ChunkRequest(markdown=markdown, source_file="FAQ.pdf")
        )
        self.assertEqual([c.category for c in result.chunks], ["General", "TapeOut Services"])
        self.assertEqual(result.chunks[1].category, "TapeOut Services")
        self.assertNotIn("General", result.chunks[1].text)
        compact = result.chunks[1].model_dump(exclude_none=True)
        self.assertNotIn("normalized_sha256", compact)
        self.assertNotIn("schedule_kind", compact)
        self.assertNotIn("part_count", compact)

    async def test_schedule_preview_filters_typed_csv(self) -> None:
        upload = UploadFile(
            file=BytesIO(
                b"technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date\n"
                b"28,HPC+ RF,8/26/2026,9/2/2026,9/9/2026,12/17/2026\n"
                b"28,HPC+ RF,10/28/2026,11/4/2026,11/11/2026,2/18/2027\n"
            ),
            filename="full_schedules.csv",
        )
        async def read(size: int = -1) -> bytes:
            return upload.file.read(size)
        upload.read = read
        result = await routes.schedule_preview(upload, 28, "HPC+ RF", "2026-10-01", "full")
        self.assertEqual(result.total_records, 2)
        self.assertEqual(result.matched_records, 1)
        self.assertEqual(result.records[0].tapeout_date.isoformat(), "2026-11-11")
        self.assertEqual(result.records[0].schedule_kind, "full")

    async def test_schedule_preview_accepts_original_export_header(self) -> None:
        upload = UploadFile(
            file=BytesIO(
                b"Tech (nm),Flavor,Trial,Final,Tapeout,Est. Ship,\n"
                b"28,HPC+ RF,8/26/2026,9/2/2026,9/9/2026,12/17/2026,\n"
            ),
            filename="shared_schedules.csv",
        )
        async def read(size: int = -1) -> bytes:
            return upload.file.read(size)
        upload.read = read
        result = await routes.schedule_preview(upload, 28, "hpc+ rf", None, "auto")
        self.assertEqual(result.total_records, 1)
        self.assertEqual(result.matched_records, 1)
        self.assertEqual(result.records[0].source_row_number, 2)
        self.assertEqual(result.schedule_kind, "shared")

    async def test_generic_csv_schedule_label_is_in_both_outputs(self) -> None:
        data = (
            b"technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date\n"
            b"28,HPC+ RF,8/26/2026,9/2/2026,9/9/2026,12/17/2026\n"
        )
        def upload():
            item = UploadFile(file=BytesIO(data), filename="no name in file.csv")
            async def read(size: int = -1) -> bytes:
                return item.file.read(size)
            item.read = read
            return item

        parsed = await routes.parse_preview(upload(), "shared")
        structured = await routes.schedule_preview(upload(), None, None, None, "shared")
        self.assertTrue(parsed.markdown.startswith("# Shared Schedules"))
        self.assertEqual(parsed.filename, "no name in file.csv")
        self.assertEqual(structured.records[0].schedule_kind, "shared")
        self.assertEqual(structured.warnings, [])
        self.assertEqual(structured.records[0].source_id, parsed.metadata.source_id)
        self.assertEqual(structured.records[0].source_row_number, 2)
        self.assertEqual(structured.retrieval_channel, "structured")
        self.assertEqual(structured.schedule_kind_source, "user")

    async def test_unknown_schedule_cannot_enter_either_channel(self) -> None:
        data = (
            b"technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date\n"
            b"28,HPC+ RF,8/26/2026,9/2/2026,9/9/2026,12/17/2026\n"
        )
        def upload():
            item = UploadFile(file=BytesIO(data), filename="no name in file.csv")
            async def read(size: int = -1) -> bytes:
                return item.file.read(size)
            item.read = read
            return item
        parsed = await routes.parse_preview(upload(), "auto")
        self.assertEqual(parsed.metadata.classification_status, "needs_schedule_kind")
        with self.assertRaises(HTTPException) as semantic_error:
            await routes.chunk_preview(ChunkRequest(
                markdown=parsed.markdown, source_file=parsed.filename,
                metadata=parsed.metadata, semantic_schedule_rows=True,
            ))
        self.assertEqual(semantic_error.exception.status_code, 422)
        with self.assertRaises(HTTPException) as structured_error:
            await routes.schedule_preview(upload(), None, None, None, "auto")
        self.assertEqual(structured_error.exception.status_code, 422)

    async def test_rejects_unsupported_file_type(self) -> None:
        upload = UploadFile(file=BytesIO(b"not supported"), filename="notes.txt")

        with self.assertRaises(HTTPException) as failure:
            await routes.parse_preview(upload)

        self.assertEqual(failure.exception.status_code, 415)

    async def test_corrupt_pdf_is_a_client_error(self) -> None:
        upload = UploadFile(file=BytesIO(b"not a PDF"), filename="manual.pdf")
        async def read(size: int = -1) -> bytes:
            return upload.file.read(size)
        upload.read = read
        with self.assertRaises(HTTPException) as failure:
            await routes.parse_preview(upload)
        self.assertEqual(failure.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
