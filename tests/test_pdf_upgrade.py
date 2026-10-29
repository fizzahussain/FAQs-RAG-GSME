"""Small PDFs exercise the fallback without depending on model downloads."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf

from gsme_rag.chunking import chunk_markdown
from gsme_rag.models import ParseWarning, ParsedDocument
from gsme_rag.parsing import parse_to_markdown
from gsme_rag.parsing.docling_pdf import _normalize_document
from gsme_rag.parsing.pdf import (
    _Line, _detect_table_layout, _recover_sparse_docling_pages,
    _remove_repeated_pdf_furniture,
)
from gsme_rag.parsing.schedules import resolve_schedule_kind


class PdfUpgradeTests(unittest.TestCase):
    def test_numbered_two_column_process_is_not_misclassified_as_faq(self):
        lines = [
            _Line(1, f"{number} Process step", 30, number * 30, 120, number * 30 + 10, 12, 1)
            for number in range(1, 5)
        ] + [
            _Line(1, f"Technical detail {number}", 260, number * 20, 390, number * 20 + 10, 11, 0)
            for number in range(1, 8)
        ]
        self.assertFalse(_detect_table_layout(lines, 600))

    def test_ambiguous_schedule_filename_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "both Full and Shared"):
            resolve_schedule_kind("full_shared_schedule.csv")

    def test_schedule_parse_rejects_rows_that_exact_search_cannot_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared_schedule.csv"
            path.write_text(
                "technology_nm,flavor,trial_deadline,final_deadline,tapeout_date,estimated_ship_date\n"
                "12,HPC+,broken,2027-02-01,2027-03-01,2027-04-01\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "row 2: trial_deadline"):
                parse_to_markdown(path)

    def test_ocr_review_marker_stays_on_its_own_page(self):
        markdown = (
            "<!-- page: 1 -->\n\n<!-- review-required: ocr -->\n\n"
            "Unverified 0.127 mm pitch.\n\n<!-- review-end -->\n\n"
            "<!-- page: 2 -->\n\nVerified process step."
        )
        result = chunk_markdown(markdown, "scanned.pdf")
        self.assertEqual([chunk.review_required for chunk in result.chunks], [True, False])
        self.assertEqual(sum(w.code == "review_required" for w in result.warnings), 1)

    def test_formula_table_rows_are_flagged_without_flagging_following_prose(self):
        markdown = (
            "<!-- page: 1 -->\n\n<!-- review-required: formula-table -->\n\n"
            "| Parameter | Formula |\n| --- | --- |\n| R | T / P |\n\n"
            "<!-- review-end -->\n\nAn ordinary paragraph."
        )
        chunks = chunk_markdown(markdown, "formulas.pdf").chunks
        self.assertTrue(next(chunk for chunk in chunks if chunk.chunk_type == "table_row").review_required)
        self.assertFalse(next(chunk for chunk in chunks if "ordinary paragraph" in chunk.text).review_required)

    def test_docling_picture_ocr_is_flagged_without_flagging_next_page(self):
        def item(label, page, text="", caption=""):
            return SimpleNamespace(label=label, prov=[SimpleNamespace(page_no=page)],
                                   text=text, caption_text=lambda doc: caption)

        class Document:
            def iterate_items(self):
                yield item("picture", 1, caption="Design graph"), 0
                yield item("text", 1, text="Suspect 0.127 mm"), 1
                yield item("text", 2, text="Next page verified text."), 0

        parsed = _normalize_document(Document(), "sample.pdf", set())
        chunks = chunk_markdown(parsed.markdown, parsed.filename).chunks
        self.assertTrue(any(chunk.review_required and "0.127" in chunk.text for chunk in chunks))
        self.assertTrue(any(not chunk.review_required and "Next page" in chunk.text for chunk in chunks))
        self.assertEqual([w.code for w in parsed.warnings].count("ocr_needs_review"), 1)

    def test_docling_tables_and_adjacent_opening_headings_remain_reviewable_evidence(self):
        class TableItem:
            label = "table"
            prov = [SimpleNamespace(page_no=1)]

            @staticmethod
            def export_to_markdown(doc):
                return "| Lot | Yield |\n| --- | --- |\n| A17 | 92 percent |"

        class Document:
            def iterate_items(self):
                yield SimpleNamespace(label="section_header", prov=[SimpleNamespace(page_no=1)],
                                      text="Opening context"), 1
                yield SimpleNamespace(label="section_header", prov=[SimpleNamespace(page_no=1)],
                                      text="Chapter 1"), 1
                yield TableItem(), 1

        parsed = _normalize_document(Document(), "sample.pdf", set())
        self.assertIn("**Opening context**", parsed.markdown)
        self.assertIn("<!-- review-required: docling-table -->", parsed.markdown)
        self.assertIn("table_needs_review", [warning.code for warning in parsed.warnings])
        table_chunk = next(c for c in chunk_markdown(parsed.markdown, parsed.filename).chunks
                           if c.chunk_type == "table_row")
        self.assertTrue(table_chunk.review_required)

    def test_docling_chapter_clears_previous_page_context_and_visual_page_is_reviewed(self):
        def item(label, page, text="", caption=""):
            return SimpleNamespace(label=label, prov=[SimpleNamespace(page_no=page)], text=text,
                                   caption_text=lambda doc: caption)

        class Document:
            def iterate_items(self):
                yield item("section_header", 1, "2.1 Expected behavior"), 1
                yield item("text", 1, "Page two details."), 1
                yield item("section_header", 2, "Charts and formulas need review"), 1
                yield item("section_header", 2, "Chapter 3 Numeric evidence boundary"), 1
                yield item("picture", 2, caption="Chart caption"), 1

        parsed = _normalize_document(Document(), "sample.pdf", set(), {2})
        result = chunk_markdown(parsed.markdown, parsed.filename)
        opening = next(c for c in result.chunks if "Charts and formulas" in c.text)
        self.assertNotIn("2.1 Expected behavior", opening.heading_path)
        page_two = [c for c in result.chunks if c.source_page_start == 2]
        self.assertTrue(any(c.review_required and "Visual evidence" in c.text for c in page_two))
        self.assertTrue(any(not c.review_required and "Charts and formulas" in c.text for c in page_two))

    def test_concatenated_review_end_and_page_marker_keep_page_provenance(self):
        markdown = (
            "<!-- page: 2 -->\n\n<!-- review-required: pdf-image-ocr -->\n\n"
            "Image text.\n\n<!-- review-end --><!-- page: 3 -->\n\n"
            "**Charts and formulas need review**\n\n# Chapter 3\n\nPage three evidence."
        )
        result = chunk_markdown(markdown, "historical-docling.md")
        page_three = [chunk for chunk in result.chunks if "Page three" in chunk.text]
        self.assertEqual(len(page_three), 1)
        self.assertEqual(page_three[0].source_page_start, 3)

    def test_docling_picture_caption_is_emitted_once(self):
        def item(label, page, text="", caption=""):
            return SimpleNamespace(label=label, prov=[SimpleNamespace(page_no=page)], text=text,
                                   caption_text=lambda doc: caption)

        caption = "Figure 2. Synthetic chart - spatial relationships require review"

        class Document:
            def iterate_items(self):
                yield item("picture", 1, caption=caption), 1
                yield item("caption", 1, text=caption), 2

        parsed = _normalize_document(Document(), "sample.pdf", set())
        self.assertEqual(parsed.markdown.count(caption), 1)

    def test_docling_caption_before_picture_is_emitted_once(self):
        caption = "Figure 2. Synthetic chart"

        class Document:
            def iterate_items(self):
                yield SimpleNamespace(label="caption", prov=[SimpleNamespace(page_no=1)],
                                      text=caption), 1
                yield SimpleNamespace(label="picture", prov=[SimpleNamespace(page_no=1)], text="",
                                      caption_text=lambda doc: caption), 1

        parsed = _normalize_document(Document(), "sample.pdf", set())
        self.assertEqual(parsed.markdown.count(caption), 1)

    def test_docling_caption_mislabeled_as_text_is_emitted_once(self):
        caption = "Table 32 ATE-Hardware"

        class Document:
            def iterate_items(self):
                yield SimpleNamespace(label="text", prov=[SimpleNamespace(page_no=1)],
                                      text=caption), 1
                yield SimpleNamespace(label="picture", prov=[SimpleNamespace(page_no=1)], text="",
                                      caption_text=lambda doc: caption), 1

        parsed = _normalize_document(Document(), "sample.pdf", set())
        self.assertEqual(parsed.markdown.count(caption), 1)

    def test_two_docling_pictures_with_same_caption_emit_it_once(self):
        caption = "Table 32 ATE-Hardware"

        class Document:
            def iterate_items(self):
                for _ in range(2):
                    yield SimpleNamespace(label="picture", prov=[SimpleNamespace(page_no=1)], text="",
                                          caption_text=lambda doc: caption), 1

        parsed = _normalize_document(Document(), "sample.pdf", set())
        self.assertEqual(parsed.markdown.count(caption), 1)

    def test_docling_table_caption_and_following_text_are_deduplicated(self):
        caption = "Table 32 ATE-Hardware"

        class TableItem:
            label = "table"
            prov = [SimpleNamespace(page_no=1)]

            @staticmethod
            def export_to_markdown(doc):
                return f"{caption}\n\n| Device | Use |\n| --- | --- |\n| PXI | Test |"

        class Document:
            def iterate_items(self):
                yield TableItem(), 1
                yield SimpleNamespace(label="text", prov=[SimpleNamespace(page_no=1)],
                                      text=caption), 1

        parsed = _normalize_document(Document(), "sample.pdf", set())
        self.assertEqual(parsed.markdown.count(caption), 1)

    def test_docling_repeated_logo_page_does_not_create_visual_placeholder(self):
        class Document:
            def iterate_items(self):
                for page in (1, 2):
                    yield SimpleNamespace(label="picture", prov=[SimpleNamespace(page_no=page)], text="",
                                          caption_text=lambda doc: ""), 1
                    yield SimpleNamespace(label="text", prov=[SimpleNamespace(page_no=page)],
                                          text=f"Page {page} prose"), 1

        parsed = _normalize_document(Document(), "sample.pdf", set(), {2})
        self.assertNotIn("Visual evidence on page 1", parsed.markdown)
        self.assertIn("Visual evidence on page 2", parsed.markdown)

    def test_docling_furniture_is_removed_and_metadata_retained_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.pdf"
            pdf = pymupdf.open()
            for number in range(1, 4):
                page = pdf.new_page(width=612, height=792)
                page.insert_text((430, 50), "MANUFACTURING DOCUMENT")
                page.insert_text((430, 65), "DOC NO: MFG-TEST-001")
                page.insert_text((72, 720), f"{number} CONFIDENTIAL: This document contains proprietary information")
                page.insert_text((72, 200), f"Unique page {number} evidence")
            pdf.save(path)
            pdf.close()
            with pymupdf.open(path) as opened:
                markdown = "\n\n".join(
                    f"<!-- page: {number} -->\n\nMANUFACTURING DOCUMENT\n\n"
                    f"DOC NO: MFG-TEST-001\n\n{number}\n\nUnique page {number} evidence"
                    for number in range(1, 4)
                )
                cleaned, removed = _remove_repeated_pdf_furniture(markdown, opened)
            self.assertGreater(removed, 0)
            self.assertEqual(cleaned.count("DOC NO: MFG-TEST-001"), 1)
            self.assertEqual(cleaned.count("MANUFACTURING DOCUMENT"), 1)
            self.assertIn("Unique page 3 evidence", cleaned)

    def test_sparse_docling_page_recovers_selectable_text_for_review(self):
        converted = ParsedDocument(
            filename="manual.pdf", document_type="pdf",
            strategy_used="docling_layout_ocr_to_markdown",
            markdown="<!-- page: 1 -->\n\nTiny fragment.\n",
        )
        lines = [
            _Line(1, f"1.{number} Selectable table of contents entry with useful context", 72,
                  90 + number * 20, 520, 105 + number * 20, 11, 0)
            for number in range(1, 7)
        ]
        recovered = _recover_sparse_docling_pages(converted, lines)
        self.assertIn("Selectable page text omitted by Docling", recovered.markdown)
        self.assertIn("1.6 Selectable table of contents", recovered.markdown)
        self.assertIn("docling_page_text_recovered", [warning.code for warning in recovered.warnings])
        chunks = chunk_markdown(recovered.markdown, recovered.filename).chunks
        self.assertTrue(any(chunk.review_required and "1.6 Selectable" in chunk.text for chunk in chunks))

    def test_docling_multiple_pictures_emit_one_visual_gap_per_page(self):
        def picture(caption):
            return SimpleNamespace(
                label="picture", prov=[SimpleNamespace(page_no=4)], text="",
                caption_text=lambda doc: caption,
            )

        class Document:
            def iterate_items(self):
                yield picture("Figure 1. First view"), 1
                yield picture("Figure 2. Second view"), 1

        parsed = _normalize_document(Document(), "sample.pdf", set(), {4})
        self.assertEqual(parsed.markdown.count("Visual evidence on page 4"), 1)
        self.assertEqual([w.code for w in parsed.warnings].count("figure_needs_review"), 1)
        self.assertEqual([w.code for w in parsed.warnings].count("visual_evidence_missing"), 1)
        self.assertIn("Figure 1. First view", parsed.markdown)
        self.assertIn("Figure 2. Second view", parsed.markdown)

    def test_generic_table_has_header_and_independent_row_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.pdf"
            pdf = pymupdf.open()
            page = pdf.new_page()
            xs, ys = (40, 170, 300), (65, 95, 125, 155)
            for x in xs:
                page.draw_line((x, ys[0]), (x, ys[-1]))
            for y in ys:
                page.draw_line((xs[0], y), (xs[-1], y))
            for row, values in enumerate((("Category", "Value"), ("Alpha", "1"), ("Beta", "2"))):
                for col, value in enumerate(values):
                    page.insert_text((xs[col] + 5, ys[row] + 20), value)
            pdf.save(path)
            pdf.close()
            with patch("gsme_rag.parsing.docling_pdf.parse_docling_pdf", side_effect=ImportError):
                parsed = parse_to_markdown(path)
            self.assertIn("| Category | Value |", parsed.markdown)
            chunks = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata).chunks
            rows = [chunk for chunk in chunks if chunk.chunk_type == "table_row"]
            self.assertEqual(len(rows), 2)
            self.assertIn("Alpha", rows[0].text)
            self.assertNotIn("Beta", rows[0].text)

    def test_large_pdf_uses_native_route_without_loading_docling(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "many-pages.pdf"
            pdf = pymupdf.open()
            page = pdf.new_page()
            for x in (40, 170, 300):
                page.draw_line((x, 65), (x, 125))
            for y in (65, 95, 125):
                page.draw_line((40, y), (300, y))
            page.insert_text((45, 85), "Parameter")
            page.insert_text((175, 85), "Value")
            page.insert_text((45, 115), "Alpha")
            page.insert_text((175, 115), "1")
            for _ in range(12):
                pdf.new_page()
            pdf.save(path)
            pdf.close()
            with patch("gsme_rag.parsing.pdf.find_spec", return_value=object()), patch(
                "gsme_rag.parsing.docling_pdf.parse_docling_pdf"
            ) as converter:
                parsed = parse_to_markdown(path, pdf_backend="auto")
            converter.assert_not_called()
            self.assertEqual(parsed.strategy_used, "pymupdf_table_fallback")
            self.assertIn("docling_skipped_large_pdf", [warning.code for warning in parsed.warnings])

    def test_empty_docling_result_falls_back_to_native_table_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fallback.pdf"
            pdf = pymupdf.open()
            page = pdf.new_page()
            for x in (40, 170, 300):
                page.draw_line((x, 65), (x, 125))
            for y in (65, 95, 125):
                page.draw_line((40, y), (300, y))
            for x, y, value in ((45, 85, "Parameter"), (175, 85, "Value"),
                                (45, 115, "Alpha"), (175, 115, "1")):
                page.insert_text((x, y), value)
            pdf.save(path)
            pdf.close()
            empty = ParsedDocument(
                filename=path.name, document_type="pdf", strategy_used="docling_layout_ocr_to_markdown",
                markdown="<!-- page: 1 -->\n", warnings=[ParseWarning("ocr_page_empty", "No text")],
            )
            with patch("gsme_rag.parsing.pdf.find_spec", return_value=object()), patch(
                "gsme_rag.parsing.docling_pdf.parse_docling_pdf", return_value=empty
            ):
                parsed = parse_to_markdown(path, pdf_backend="auto")
            self.assertEqual(parsed.strategy_used, "pymupdf_table_fallback")
            self.assertIn("Alpha", parsed.markdown)
            self.assertIn("docling_empty_fallback", [warning.code for warning in parsed.warnings])

    def test_question_does_not_absorb_later_pages(self):
        markdown = (
            "<!-- page: 1 -->\n\n## General\n\n**01 What is included?**\n\nA short answer.\n\n"
            "<!-- page: 2 -->\n\nA different topic begins here.\n\n"
            "<!-- page: 3 -->\n\nAnother chapter."
        )
        chunks = chunk_markdown(markdown, "questions.pdf").chunks
        faq = [chunk for chunk in chunks if chunk.chunk_type == "question_answer"]
        self.assertEqual(len(faq), 1)
        self.assertNotIn("different topic", faq[0].text)
        self.assertTrue(any("different topic" in chunk.text for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
