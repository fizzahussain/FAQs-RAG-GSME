import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from gsme_rag.chunking import chunk_markdown
from gsme_rag.parsing import parse_to_markdown
from gsme_rag.parsing.pdf import _extract_lines, _native_markdown
from gsme_rag.parsing.ocr import OcrUnavailable


class ManualGuardrailTests(unittest.TestCase):
    def test_bundled_validation_pdf_without_tesseract_keeps_visual_gap(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "GSME_Parsing_Validation_0.14.3.pdf"
        with patch.dict(os.environ, {"GSME_PDF_BACKEND": "native"}), patch(
            "gsme_rag.parsing.pdf.ocr_image", side_effect=OcrUnavailable("Tesseract not on PATH")
        ):
            parsed = parse_to_markdown(path)
        result = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata)
        codes = [warning.code for warning in parsed.warnings]
        self.assertIn("image_ocr_unavailable", codes)
        self.assertIn("visual_evidence_missing", codes)
        self.assertNotIn("Image text (OCR):", parsed.markdown)
        self.assertTrue(any(chunk.review_required and "Visual evidence on page 2" in chunk.text
                            for chunk in result.chunks))

    def test_bundled_validation_pdf_end_to_end(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "GSME_Parsing_Validation_0.14.3.pdf"
        with patch.dict(os.environ, {"GSME_PDF_BACKEND": "native"}), patch(
            "gsme_rag.parsing.pdf.ocr_image", return_value="OCR TEST: YIELD 92 PERCENT"
        ):
            parsed = parse_to_markdown(path)
        result = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata)
        self.assertEqual(result.content_route, "paper")
        self.assertEqual(parsed.markdown.count("<!-- page:"), 3)
        self.assertNotIn("CONFIDENTIAL TEST FOOTER", parsed.markdown)
        self.assertIn("OCR TEST: YIELD 92 PERCENT", parsed.markdown)
        self.assertTrue(any(chunk.review_required and "OCR TEST" in chunk.text for chunk in result.chunks))
        self.assertTrue(all(chunk.review_required for chunk in result.chunks
                            if chunk.source_page_start == 3))
        self.assertIn("visual_evidence_missing", [warning.code for warning in parsed.warnings])
        opening = next(chunk for chunk in result.chunks if "Charts and formulas need review" in chunk.text)
        self.assertEqual(opening.source_page_start, 3)
        self.assertNotIn("2.1 Expected behavior", opening.heading_path)

    def test_heading_context_continues_across_page_without_new_chapter(self):
        markdown = ("<!-- page: 1 -->\n# Chapter 1\n## 1.1 Process\nFirst part.\n"
                    "<!-- page: 2 -->\nSecond part of same section.")
        result = chunk_markdown(markdown)
        continuation = next(chunk for chunk in result.chunks if "Second part" in chunk.text)
        self.assertEqual(continuation.heading_path, ["Chapter 1", "1.1 Process"])

    def test_manual_questions_do_not_force_faq(self):
        markdown = "\n\n".join([
            "<!-- page: 1 -->", "# Chapter 1", "# 1.1 Overview", "Technical prose.",
            "# 1.2 Process", "More technical prose.", "# 1.3 Testing",
            "# 1.4 Assembly", "**What is a mask?**", "A mask protects traces.",
        ])
        result = chunk_markdown(markdown)
        self.assertEqual(result.content_route, "paper")
        self.assertFalse(any(chunk.chunk_type == "question_answer" for chunk in result.chunks))

    def test_nested_review_markers_do_not_clear_outer_review(self):
        markdown = ("<!-- page: 1 -->\n<!-- review-required: ocr-or-table -->\n"
                    "Before table.\n\n<!-- review-required: inferred-table -->\n"
                    "| Key | Value |\n| --- | --- |\n| A | B |\n<!-- review-end -->\n"
                    "After table.\n<!-- review-end -->")
        result = chunk_markdown(markdown)
        self.assertTrue(result.chunks)
        self.assertTrue(all(chunk.review_required for chunk in result.chunks))

    def test_repeated_margin_text_removed_but_page_markers_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.pdf"
            pdf = pymupdf.open()
            for number in range(1, 4):
                page = pdf.new_page(width=612, height=792)
                page.insert_text((50, 35), "REPEATED MANUAL HEADER")
                page.insert_text((50, 200), f"Chapter {number} unique content on this page")
                page.insert_text((50, 750), "REPEATED CONFIDENTIAL FOOTER")
                page.insert_text((540, 720), str(number))
            pdf.save(path)
            pdf.close()
            with patch.dict(os.environ, {"GSME_PDF_BACKEND": "native"}):
                parsed = parse_to_markdown(path)
            self.assertEqual(parsed.markdown.count("<!-- page:"), 3)
            self.assertNotIn("REPEATED MANUAL HEADER", parsed.markdown)
            self.assertNotIn("REPEATED CONFIDENTIAL FOOTER", parsed.markdown)
            self.assertIn("Chapter 3 unique content", parsed.markdown)

    def test_large_image_emits_reviewable_visual_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "figure.pdf"
            pdf = pymupdf.open()
            page = pdf.new_page(width=612, height=792)
            page.insert_text((50, 80), "Chapter 1 Results")
            page.insert_text((50, 120), "The results are discussed below in ordinary prose.")
            raster = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), 0)
            raster.clear_with(0xFFFFFF)
            page.insert_image(pymupdf.Rect(80, 200, 500, 600), pixmap=raster)
            pdf.save(path)
            pdf.close()
            with patch.dict(os.environ, {"GSME_PDF_BACKEND": "native"}), patch(
                "gsme_rag.parsing.pdf.ocr_image", return_value=""
            ):
                parsed = parse_to_markdown(path)
            result = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata)
            self.assertIn("visual_evidence_missing", [warning.code for warning in parsed.warnings])
            self.assertTrue(any("Visual evidence" in chunk.text and chunk.review_required
                                for chunk in result.chunks))
            self.assertTrue(any("ordinary prose" in chunk.text and not chunk.review_required
                                for chunk in result.chunks))

    def test_one_row_formula_region_retains_selectable_source_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formula.pdf"
            pdf = pymupdf.open()
            page = pdf.new_page()
            page.insert_text((60, 80), "GDPW = 3.14 times wafer area")
            pdf.save(path)
            pdf.close()
            with pymupdf.open(path) as opened:
                lines = _extract_lines(opened)
                class SingleRow:
                    bbox = (50, 50, 400, 100)
                    def extract(self):
                        return [["GDPW", "3.14 times wafer area"]]
                class Tables:
                    tables = [SingleRow()]
                with patch.object(pymupdf.Page, "find_tables", return_value=Tables()):
                    markdown, warnings = _native_markdown(opened, lines)
            self.assertIn("GDPW = 3.14 times wafer area", markdown)
            self.assertIn("table_needs_review", [warning.code for warning in warnings])
            result = chunk_markdown(markdown)
            self.assertTrue(any(chunk.review_required for chunk in result.chunks))


if __name__ == "__main__":
    unittest.main()
