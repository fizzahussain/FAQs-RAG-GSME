import asyncio
from io import BytesIO
import unittest
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from gsme_rag.api import routes
from gsme_rag.api.app import create_app
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
        self.assertEqual(await routes.health(), {"status": "ok"})
        paths = create_app().openapi()["paths"]
        self.assertIn("/v1/parse", paths)
        self.assertIn("/v1/chunk", paths)
        self.assertIn("/v1/compare", paths)
        self.assertIn("/v1/schedules/preview", paths)

    async def test_parse_returns_markdown_without_chunks(self) -> None:
        upload = UploadFile(
            file=BytesIO(b"Tech,Flavor\n28,HPC+ RF\n"),
            filename="shared_schedules.csv",
        )
        # UploadFile.read may delegate to a thread for ordinary BytesIO files.
        async def read(size: int = -1) -> bytes:
            return upload.file.read(size)

        upload.read = read
        result = await routes.parse_preview(upload)

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
        result = await routes.schedule_preview(upload, 28, "HPC+ RF", "2026-10-01")
        self.assertEqual(result.total_records, 2)
        self.assertEqual(result.matched_records, 1)
        self.assertEqual(result.records[0].tapeout_date.isoformat(), "2026-11-11")

    async def test_rejects_unsupported_file_type(self) -> None:
        upload = UploadFile(file=BytesIO(b"not supported"), filename="notes.txt")

        with self.assertRaises(HTTPException) as failure:
            await routes.parse_preview(upload)

        self.assertEqual(failure.exception.status_code, 415)


if __name__ == "__main__":
    unittest.main()
