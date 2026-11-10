"""Cases must originate in source files, not be invented from extracted output."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless

from docx import Document
import pymupdf as fitz

from gsme_rag.parsing.pdf import _substantive_drawings
from tools.audit_sources import audit
from tools.evaluate_gsme_sources import inspect_cases, load_audit


class SourceCaseTests(TestCase):
    def test_frames_underlines_and_bullets_are_not_substantive_diagrams(self):
        with fitz.open() as document:
            page = document.new_page(width=600, height=800)
            page.draw_rect(fitz.Rect(5, 5, 595, 795))
            for index in range(15):
                page.draw_line((60, 100 + index * 20), (350, 100 + index * 20))
                page.draw_rect(fitz.Rect(45, 100 + index * 20, 48, 103 + index * 20))
            self.assertEqual(_substantive_drawings(page), [])
            page.draw_rect(fitz.Rect(100, 430, 400, 600))
            self.assertEqual(len(_substantive_drawings(page)), 1)

    def test_source_case_is_checked_against_original_and_artifact_hash(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "faq.docx"
            doc = Document()
            doc.add_heading("What is the synthetic deadline?", 2)
            doc.add_paragraph("The synthetic deadline is 15 October.")
            doc.add_heading("Where is the missing schedule?", 2)
            doc.add_paragraph("The schedule can be found here.")
            doc.save(source)
            audit(source, root / "audit" / "faq_docx")
            parsed, chunks, directory = load_audit(source, root / "audit")
            inputs = {"faq": (source, parsed, chunks, directory)}
            catalog = {"cases": [
                {"id": "deadline", "source": "faq", "kind": "answer", "question": "When is it due?", "anchor": "15 October"},
                {"id": "link", "source": "faq", "kind": "hold", "question": "What is the URL?", "anchor": "The schedule can be found here"}]}
            rows = inspect_cases(catalog, inputs)
            self.assertEqual([r["source_check"] for r in rows], ["ready_for_retrieval", "held_as_required"])
            catalog["cases"][0]["anchor"] = "invented date 99 December"
            with self.assertRaisesRegex(ValueError, "absent from original"):
                inspect_cases(catalog, inputs)
            with (directory / "normalized.md").open("ab") as output:
                output.write(b"changed content")
            with self.assertRaisesRegex(ValueError, "changed after ingestion"):
                load_audit(source, root / "audit")

    @skipUnless(os.getenv("GSME_TEST_MANUAL_PDF"), "Private source PDF is optional")
    def test_actual_glossary_and_plain_procedure_pages_have_no_diagram_count_false_positive(self):
        with fitz.open(os.environ["GSME_TEST_MANUAL_PDF"]) as document:
            for number in [50, 120]:
                self.assertLess(len(_substantive_drawings(document[number - 1])), 10)
