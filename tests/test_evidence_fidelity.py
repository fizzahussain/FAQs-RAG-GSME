"""Regression gates for the manual's TOC, equations, and chart evidence."""

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import pymupdf as fitz

from gsme_rag.chunking import chunk_markdown
from gsme_rag.models import DocumentMetadata, ParsedDocument
from gsme_rag.parsing.docling_pdf import _cell_loss_evidence, _chart_label_evidence, _normalize_document
from gsme_rag.parsing.fidelity import _reason, inspect_pdf_fidelity
from gsme_rag.parsing.pdf import _Line, _extract_lines, _native_markdown, _recover_sparse_docling_pages
from gsme_rag.review import ReviewDecision, ReviewLedger, eligible_for_embedding


MANUAL = Path(os.getenv("GSME_TEST_MANUAL_PDF", "validation/source/MFG-GSME-0002.pdf"))


class EvidenceFidelityTests(unittest.TestCase):
    def test_broken_font_and_incomplete_formula_detection(self):
        self.assertIn("Unicode", _reason("Dose \ufffd 3"))
        self.assertIn("Unicode", _reason("A \ue123 B"))
        self.assertIn("Unicode", _reason("(cid:123)"))
        self.assertIn("incomplete", _reason("DPPM = ("))
        self.assertIn("layout", _reason("Yield =\n1\n(1 + A D0)n"))
        self.assertIsNone(_reason("Ordinary technical prose without a formula."))

    def test_formula_and_chart_pages_stay_reviewed_through_chunking(self):
        class FakeDocument:
            def iterate_items(self):
                for page, value in ((30, "DPPM = ("), (101, "8% 25% 27% 22% 5% 13%"),
                                    (163, "Yield = 1 (1 + A D0)n")):
                    yield SimpleNamespace(label="text", text=value,
                                          prov=[SimpleNamespace(page_no=page)]), 1

        parsed = _normalize_document(FakeDocument(), "manual.pdf", {30, 101, 163},
                                     {101}, ocr_review_pages=set())
        chunks = chunk_markdown(parsed.markdown, "manual.pdf", content_route="paper").chunks
        self.assertEqual({chunk.source_page_start for chunk in chunks}, {30, 101, 163})
        self.assertTrue(all(chunk.review_required for chunk in chunks))
        self.assertFalse(any(eligible_for_embedding(chunk) for chunk in chunks))

    def test_recovered_contents_does_not_inherit_previous_heading(self):
        parsed = ParsedDocument("manual.pdf", "pdf", "docling_layout_ocr_to_markdown",
                                "<!-- page: 1 -->\n\n# Revision History\n\nOld row.\n\n"
                                "<!-- page: 2 -->\n\nTiny fragment.\n")
        lines = [_Line(2, "Table of Contents", 70, 90, 300, 108, 12, 1)]
        lines.extend(_Line(2, f"1.{number} Section title {number} ........... {number + 10}",
                           70, 110 + number * 20, 540, 126 + number * 20, 11, 0)
                     for number in range(1, 9))
        recovered = _recover_sparse_docling_pages(parsed, lines)
        chunks = chunk_markdown(recovered.markdown, "manual.pdf", content_route="paper").chunks
        page_two = [chunk for chunk in chunks if chunk.source_page_start == 2]
        self.assertTrue(page_two)
        contents = [chunk for chunk in page_two if "1." in chunk.text]
        self.assertTrue(contents)
        self.assertTrue(all(chunk.heading_path == ["Table of Contents"] for chunk in contents))
        self.assertTrue(all(chunk.review_required for chunk in contents))

    def test_unapproved_reviewed_claims_and_generic_visual_gaps_are_not_indexable(self):
        text = ("<!-- page: 101 -->\n\n<!-- review-required: chart -->\n\n"
                "8%, 25%, 27%, 22%, 5%, 13%\n\n<!-- review-end -->\n\n"
                "<!-- section-break -->\n\n"
                "<!-- review-required: visual-evidence -->\n\n"
                "Visual evidence on page 101 requires source review; no verified description is available.\n\n"
                "<!-- review-end -->")
        digest = hashlib.sha256(text.encode()).hexdigest()
        metadata = DocumentMetadata("pdf-test", "source-hash", digest, "manual.pdf", "pdf", "document")
        chunks = chunk_markdown(text, "manual.pdf", metadata=metadata).chunks
        self.assertTrue(chunks)
        self.assertTrue(all(not eligible_for_embedding(chunk) for chunk in chunks))
        ledger = ReviewLedger([ReviewDecision("source-hash", chunks[0].chunk_id,
                                              "approved", "reviewer", "checked against source", True)])
        self.assertTrue(eligible_for_embedding(chunks[0], ledger))
        self.assertFalse(any(eligible_for_embedding(chunk, ledger) for chunk in chunks
                             if "Visual evidence on page" in chunk.text))
        with TemporaryDirectory() as directory:
            sidecar = Path(directory) / "review-decisions.json"
            ledger.save(sidecar)
            self.assertTrue(ReviewLedger.load(sidecar).approved("source-hash", chunks[0].chunk_id))

    def test_pending_prose_and_toc_navigation_are_not_automatically_approved(self):
        markdown = ("<!-- page: 2 -->\n\n# Table of Contents\n\n"
                    "1.1 Introduction ............... 10\n\n"
                    "<!-- page: 11 -->\n\n# Introduction\n\nOrdinary verified-looking prose.")
        digest = hashlib.sha256(markdown.encode()).hexdigest()
        metadata = DocumentMetadata("doc", "source-hash", digest, "manual.pdf", "pdf", "document")
        chunks = chunk_markdown(markdown, "manual.pdf", metadata=metadata, content_route="paper").chunks
        toc = next(chunk for chunk in chunks if chunk.source_page_start == 2)
        prose = next(chunk for chunk in chunks if chunk.source_page_start == 11)
        self.assertEqual(toc.evidence_status, "excluded")
        self.assertFalse(eligible_for_embedding(toc))
        self.assertEqual(prose.evidence_status, "pending")
        self.assertFalse(eligible_for_embedding(prose))
        ledger = ReviewLedger([ReviewDecision("source-hash", prose.chunk_id,
                                              "approved", "reviewer", "page checked", True)])
        self.assertTrue(eligible_for_embedding(prose, ledger))
        self.assertEqual(ledger.status_for(prose), "approved")
        self.assertFalse(eligible_for_embedding(toc, ledger))
        self.assertEqual(ledger.status_for(toc), "excluded")

    def test_toc_heading_leak_does_not_exclude_later_body(self):
        markdown = ("<!-- page: 2 -->\n\n# Table of Contents\n\n"
                    "1.1 Introduction ............... 10\n\n"
                    "<!-- page: 30 -->\n\nBody prose without navigation dots.")
        chunks = chunk_markdown(markdown, "manual.pdf", content_route="paper").chunks
        later = next(chunk for chunk in chunks if chunk.source_page_start == 30)
        self.assertEqual(later.evidence_status, "pending")

    @unittest.skipUnless(MANUAL.is_file(), "Exact source manual is not available")
    def test_native_page_30_formula_is_quarantined(self):
        with fitz.open(MANUAL) as source, fitz.open() as one_page:
            one_page.insert_pdf(source, from_page=29, to_page=29)
            evidence, warnings, pages = inspect_pdf_fidelity(one_page, max_ocr_regions=0)
            markdown, native_warnings = _native_markdown(one_page, _extract_lines(one_page), pages)
        chunks = chunk_markdown(markdown, "manual-page-30.pdf", content_route="paper").chunks
        formula = [chunk for chunk in chunks if "DPPM = (" in chunk.text]
        self.assertTrue(formula)
        self.assertTrue(all(chunk.review_required for chunk in formula))
        self.assertTrue(all(not eligible_for_embedding(chunk) for chunk in formula))
        self.assertTrue(any(item.kind == "formula_unavailable" for item in evidence))
        self.assertTrue(any(item.code == "formula_unavailable" for item in warnings))

    @unittest.skipUnless(MANUAL.is_file(), "Exact source manual is not available")
    def test_page_two_dropped_grid_cells_are_located_and_preserved(self):
        with fitz.open(MANUAL) as source:
            loss = {
                "bbox": [142, 202, 1082, 1380], "page_width": 1224, "page_height": 1584,
                "pdf_cell_count": 183, "grid_rows": 30, "grid_columns": 3,
                "missing_cells": [
                    {"id": 112, "text": "Gates", "bbox": [237, 384, 289, 405]},
                    {"id": 191, "text": "Edge/Width", "bbox": [306, 929, 414, 950]},
                ],
            }
            loss["all_cells"] = loss["missing_cells"] + [
                {"id": 90, "text": "Introduction", "bbox": [250, 260, 340, 280]}]
            evidence, warnings, pages = _cell_loss_evidence(source, [loss])
        self.assertEqual(pages, {2})
        self.assertEqual(len(evidence), 3)
        self.assertEqual(sum(item.kind == "table_cell_drop" for item in evidence), 2)
        self.assertEqual(warnings[0].code, "docling_table_cells_dropped")
        self.assertIn("30x3", warnings[0].message)
        self.assertIn("page 2", warnings[0].location)
        self.assertEqual(next(item for item in evidence if item.original_text == "Gates").bbox,
                         (118.5, 192.0, 144.5, 202.5))

    @unittest.skipUnless(MANUAL.is_file(), "Exact source manual is not available")
    def test_source_equation_regions_and_unmapped_chart_labels(self):
        with fitz.open(MANUAL) as source:
            evidence, warnings, pages = inspect_pdf_fidelity(source, max_ocr_regions=0)
            chart = _chart_label_evidence(source, {101})
        self.assertIn(30, pages)
        self.assertIn(163, pages)
        self.assertTrue(any(item.page == 30 and "DPPM = (" in item.original_text
                            and item.bbox for item in evidence))
        self.assertTrue(any(item.page == 163 and "Yield =" in item.original_text
                            and item.bbox for item in evidence))
        self.assertEqual(len(chart), 1)
        self.assertEqual(chart[0].page, 101)
        self.assertEqual(len(chart[0].original_text.split(", ")), 6)
        self.assertIn("unverified", chart[0].reason)


if __name__ == "__main__":
    unittest.main()
