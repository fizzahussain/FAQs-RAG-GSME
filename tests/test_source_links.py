"""Source hyperlink fidelity: do not turn an unlinked 'here' into a guessed URL."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import pymupdf as fitz
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from gsme_rag.parsing import parse_to_markdown
from gsme_rag.parsing.links import markdown_link, safe_link_target, missing_link_reference, missing_link_warnings
from gsme_rag.chunking import chunk_markdown
from gsme_rag.models import ParsedDocument


URL = "https://example.invalid/shared?run=(A17)&type=shared"


def add_hyperlink(paragraph, label="here", target=URL):
    element = OxmlElement("w:hyperlink")
    element.set(qn("r:id"), paragraph.part.relate_to(target, RT.HYPERLINK, is_external=True))
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = label
    run.append(text)
    element.append(run)
    paragraph._p.append(element)


class SourceLinkTests(TestCase):
    def test_missing_link_warning_reaches_unpaged_docx_review_card(self):
        from types import SimpleNamespace
        from gsme_rag.storage.postgres import _page_warning_codes
        chunks = chunk_markdown("# Schedule\n\nWhere is the schedule?\n\nIt can be found here.", "source.docx").chunks
        unresolved = next(chunk for chunk in chunks if "found here" in chunk.text)
        self.assertEqual(_page_warning_codes(SimpleNamespace(warnings=[]), unresolved), ["link_target_missing"])

    def test_local_demo_never_downloads_tokenizer_data(self):
        from gsme_rag.storage.providers import FakeEmbeddings
        with patch("socket.getaddrinfo", side_effect=AssertionError("No network in local demo")):
            provider = FakeEmbeddings(16)
            self.assertEqual(provider.encode_tokens("LOT A17"), list(b"LOT A17"))
            self.assertEqual(len(provider.embed_query("LOT A17")), 16)

    def test_docx_table_retains_link_label_target_and_chunk_question(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "faq.docx"
            doc = Document()
            table = doc.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "Question"
            table.cell(0, 1).text = "Answer"
            table.cell(1, 0).text = "Where is the Shared Block schedule?"
            paragraph = table.cell(1, 1).paragraphs[0]
            paragraph.add_run("The schedule can be found ")
            add_hyperlink(paragraph)
            paragraph.add_run(".")
            doc.save(source)
            parsed = parse_to_markdown(source)
            chunks = chunk_markdown(parsed.markdown, parsed.filename, metadata=parsed.metadata).chunks
            self.assertIn(markdown_link("here", URL), parsed.markdown)
            self.assertEqual(len(chunks), 1)
            self.assertIn(URL, chunks[0].text)
            self.assertIn("Where is the Shared Block schedule?", chunks[0].text)
            self.assertFalse(any(w.code == "link_target_missing" for w in parsed.warnings))

    def test_docx_prose_link_does_not_lose_neighboring_runs(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "prose.docx"
            doc = Document()
            doc.add_heading("Where is the schedule?", 2)
            paragraph = doc.add_paragraph("Before ")
            add_hyperlink(paragraph)
            paragraph.add_run(" after.")
            doc.save(source)
            parsed = parse_to_markdown(source)
            self.assertIn(f"Before {markdown_link('here', URL)} after.", parsed.markdown)

    def test_native_pdf_uri_annotation_enters_same_source_chunk(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "linked.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page()
                page.insert_text((50, 100), "Our schedule can be found here.")
                page.insert_link({"kind": fitz.LINK_URI, "from": page.search_for("here")[0], "uri": URL})
                pdf.save(source)
            parsed = parse_to_markdown(source, pdf_backend="native")
            chunks = chunk_markdown(parsed.markdown, parsed.filename, metadata=parsed.metadata).chunks
            self.assertIn(URL, parsed.markdown)
            self.assertIn(URL, chunks[0].text)
            self.assertEqual(chunks[0].source_page_start, 1)
            self.assertNotIn("pdf_link_placement_review", [w.code for w in parsed.warnings])

    def test_pdf_link_hook_also_applies_to_docling_result(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "linked.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page()
                page.insert_text((50, 100), "Our schedule can be found here.")
                page.insert_link({"kind": fitz.LINK_URI, "from": page.search_for("here")[0], "uri": URL})
                pdf.save(source)
            result = ParsedDocument(source.name, "pdf", "docling_layout_ocr_to_markdown", "<!-- page: 1 -->\nOur schedule can be found here.\n")
            with patch("gsme_rag.parsing.pdf._parse_pdf_to_markdown", return_value=result):
                parsed = parse_to_markdown(source, pdf_backend="docling")
            self.assertIn(URL, parsed.markdown)
            self.assertEqual(parsed.strategy_used, "docling_layout_ocr_to_markdown")

    def test_unlinked_pdf_here_is_not_replaced_with_invented_url(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "unlinked.pdf"
            with fitz.open() as pdf:
                pdf.new_page().insert_text((50, 100), "Our schedule can be found here.")
                pdf.save(source)
            parsed = parse_to_markdown(source, pdf_backend="native")
            chunks = chunk_markdown(parsed.markdown, parsed.filename, metadata=parsed.metadata).chunks
            self.assertNotIn("https://", parsed.markdown)
            self.assertIn("link_target_missing", [w.code for w in parsed.warnings])
            self.assertTrue(chunks[0].review_required)

    def test_repeated_here_annotations_remain_reviewed_not_wrongly_assigned(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "ambiguous.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page()
                page.insert_text((50, 100), "Choose here or here.")
                for index, rect in enumerate(page.search_for("here")):
                    page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": f"https://example.invalid/{index}"})
                pdf.save(source)
            parsed = parse_to_markdown(source, pdf_backend="native")
            chunks = chunk_markdown(parsed.markdown, parsed.filename, metadata=parsed.metadata).chunks
            self.assertIn("pdf_link_placement_review", [w.code for w in parsed.warnings])
            self.assertTrue(all(c.review_required for c in chunks if "https://" in c.text))
            self.assertTrue(all(c.source_page_start == 1 for c in chunks))
            self.assertIn("https://example.invalid/0", parsed.markdown)
            self.assertIn("https://example.invalid/1", parsed.markdown)

    def test_dangerous_schemes_and_credentials_are_not_clickable_markdown(self):
        for target in ("javascript:alert(1)", "data:text/html,x", "file:///C:/private", "https://user:secret@example.invalid", "https://example.invalid/\ntext"):
            with self.subTest(target=target):
                self.assertFalse(safe_link_target(target))
                self.assertEqual(markdown_link("here", target), "here")
        self.assertTrue(safe_link_target("mailto:support@example.invalid"))

    def test_missing_link_detection_preserves_page_table_and_code_context(self):
        self.assertTrue(missing_link_reference("The schedule can be found here."))
        self.assertFalse(missing_link_reference(f"The schedule can be found {markdown_link('here', URL)}."))
        self.assertFalse(missing_link_reference('```\nprint("click here")\n```'))
        self.assertEqual(missing_link_warnings('```\nprint("click here")\n```'), [])
        markdown = "| Question | Answer | Page |\n|---|---|---|\n| Where? | It can be found here. | 4 |"
        self.assertEqual(missing_link_warnings(markdown)[0].location, "page 4")
