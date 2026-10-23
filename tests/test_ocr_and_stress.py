"""Regression cases for images, review provenance, routing and larger input."""
from io import BytesIO
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
from docx.shared import Inches
import pymupdf

from gsme_rag.chunking import chunk_markdown
from gsme_rag.parsing import parse_to_markdown
from gsme_rag.parsing.ocr import OcrUnavailable, find_tesseract, ocr_image
from gsme_rag.api.app import app
from fastapi.testclient import TestClient


def sample_png(text: str) -> bytes:
    pdf = pymupdf.open()
    page = pdf.new_page(width=650, height=240)
    page.insert_text((25, 95), text, fontsize=25)
    png = page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    pdf.close()
    return png


class OCRStressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @unittest.skipUnless(find_tesseract(), "Tesseract is required for OCR integration")
    def test_docx_image_ocr_and_review_flag(self):
        path = self.root / "figure.docx"
        doc = Document()
        doc.add_heading("Results", 1)
        doc.add_paragraph("Scanned caption follows.")
        doc.add_picture(BytesIO(sample_png("Yield 87 percent")), width=Inches(4))
        doc.save(path)
        parsed = parse_to_markdown(path)
        self.assertIn("Yield 87", parsed.markdown)
        chunks = chunk_markdown(parsed.markdown, parsed.filename, metadata=parsed.metadata).chunks
        self.assertTrue(any(chunk.review_required for chunk in chunks))
        self.assertTrue(any("Yield" in chunk.text for chunk in chunks))

    @unittest.skipUnless(find_tesseract(), "Tesseract is required for OCR integration")
    def test_scanned_pdf_page_is_reviewable(self):
        path = self.root / "scan.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=650, height=240)
        page.insert_image(page.rect, stream=sample_png("Tapeout date 2027"))
        doc.save(path)
        doc.close()
        parsed = parse_to_markdown(path)
        self.assertIn("2027", parsed.markdown)
        chunks = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata).chunks
        self.assertTrue(all(c.review_required for c in chunks))
        self.assertEqual(chunks[0].source_page_start, 1)

    def test_ocr_engine_missing_emits_warning(self):
        with patch("gsme_rag.parsing.ocr.find_tesseract", return_value=None):
            with self.assertRaises(OcrUnavailable):
                ocr_image(sample_png("Figure 3"))

    @unittest.skipUnless(find_tesseract(), "Tesseract is required for OCR integration")
    def test_validation_raster_preserves_lot_a17(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "GSME_Parsing_Validation_0.14.3.pdf"
        with pymupdf.open(path) as document:
            image = next(item for item in document[1].get_image_info(xrefs=True) if item.get("xref"))
            image_bytes = document.extract_image(image["xref"])["image"]
        recognized = ocr_image(image_bytes)
        self.assertIn("LOT A17", recognized)
        self.assertNotIn("LOT Al7", recognized)

    def test_two_column_numbered_paper_is_not_faq(self):
        path = self.root / "paper.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        for i in range(1, 5):
            y = 70 + 85 * i
            page.insert_text((50, y), f"{i} Methodology", fontname="hebo", fontsize=12)
            page.insert_text((280, y), "Measurement and results are described.", fontsize=10)
            page.insert_text((280, y + 17), "These values need context.", fontsize=10)
        doc.save(path)
        doc.close()
        parsed = parse_to_markdown(path)
        self.assertNotEqual(parsed.strategy_used, "pdf_table_layout_to_markdown")

    def test_large_faq_table_keeps_each_question_with_own_answer(self):
        rows = ["| Category | Question | Answer |", "| --- | --- | --- |"]
        rows += [f"| General | What is item {i}? | Answer item {i} is available. |" for i in range(1000)]
        result = chunk_markdown("\n".join(rows), max_chars=250)
        self.assertEqual(len(result.chunks), 1000)
        self.assertTrue(all(c.faq_question for c in result.chunks))
        self.assertEqual(len({c.chunk_id for c in result.chunks}), 1000)
        self.assertIn("Answer item 999", result.chunks[-1].text)

    def test_review_image_cannot_become_previous_faq_answer(self):
        markdown = ("# General\n\n**What is the date?**\n\nThe date is in the table.\n\n"
                    "<!-- review-required: docx-image-ocr -->\nFuzzy date maybe 2027\n<!-- review-end -->")
        result = chunk_markdown(markdown)
        self.assertEqual(result.chunks[0].chunk_type, "question_answer")
        self.assertFalse(result.chunks[0].review_required)
        self.assertNotIn("Fuzzy date", result.chunks[0].text)
        self.assertTrue(result.chunks[1].review_required)

    def test_mixed_faq_and_paper_uses_local_heading_context(self):
        markdown = ("# General\n\n**What is Muse?**\n\nMuse offers services.\n\n"
                    "# Methods\n\n**Why might the values vary?**\n\nThis paper measures variation.\n\n"
                    "## Experiment\n\nFurther results are discussed here.")
        result = chunk_markdown(markdown)
        self.assertEqual(result.content_route, "mixed")
        self.assertEqual(sum(c.chunk_type == "question_answer" for c in result.chunks), 1)
        self.assertTrue(any(c.chunk_type == "section" and "values vary" in c.text for c in result.chunks))

    def test_native_pdf_table_is_row_chunked_and_reviewable(self):
        path = self.root / "paper_table.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=400)
        positions = [40, 170, 300]
        values = [("Metric", "Value"), ("Yield", "87%"), ("Area", "2 mm2")]
        for i, (a, b) in enumerate(values):
            y = 70 + 40 * i
            page.insert_text((positions[0], y), a, fontsize=11)
            page.insert_text((positions[1], y), b, fontsize=11)
        for x in positions:
            page.draw_line((x - 8, 42), (x - 8, 170))
        for y in (45, 85, 125, 165):
            page.draw_line((32, y), (292, y))
        doc.save(path)
        doc.close()
        with patch.dict("os.environ", {"GSME_PDF_BACKEND": "native"}):
            parsed = parse_to_markdown(path)
        self.assertIn("| Metric | Value |", parsed.markdown)
        result = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata)
        self.assertEqual([c.chunk_type for c in result.chunks], ["table_row", "table_row"])
        self.assertTrue(all(c.review_required for c in result.chunks))

    def test_many_page_text_pdf_keeps_page_provenance(self):
        path = self.root / "large.pdf"
        doc = pymupdf.open()
        for i in range(80):
            page = doc.new_page()
            page.insert_text((40, 70), f"Page {i + 1} distinct semiconductor information", fontsize=11)
        doc.save(path)
        doc.close()
        parsed = parse_to_markdown(path)
        chunks = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata).chunks
        self.assertEqual(chunks[0].source_page_start, 1)
        self.assertEqual(chunks[-1].source_page_start, 80)
        self.assertFalse(any(c.review_required for c in chunks))

    def test_vector_chart_with_selectable_labels_warns_about_missing_relationship(self):
        path = self.root / "vector_chart.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((40, 40), "Yield by process: measurement labels")
        for index in range(12):
            page.draw_rect(pymupdf.Rect(45 + index * 18, 100, 55 + index * 18, 130 + index))
        doc.save(path)
        doc.close()
        with patch.dict("os.environ", {"GSME_PDF_BACKEND": "native"}):
            parsed = parse_to_markdown(path)
        self.assertIn("vector_content_needs_review", [w.code for w in parsed.warnings])

    def test_api_rejects_corrupt_docx_without_server_error(self):
        response = TestClient(app).post(
            "/v1/parse", files={"file": ("bad.docx", b"not a zip", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
