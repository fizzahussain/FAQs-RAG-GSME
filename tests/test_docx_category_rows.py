from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from docx import Document
from gsme_rag.parsing import parse_to_markdown
from gsme_rag.chunking import chunk_markdown


class DocxCategoryTests(TestCase):
    def test_category_merge_does_not_taint_independent_qa_and_row_numbers_survive(self):
        with TemporaryDirectory() as temp:
            source = Path(temp) / "synthetic.docx"
            doc = Document()
            table = doc.add_table(rows=4, cols=2)
            table.cell(0, 0).text, table.cell(0, 1).text = "Question", "Answer"
            table.cell(1, 0).merge(table.cell(1, 1)).text = "General"
            table.cell(2, 0).text, table.cell(2, 1).text = "Who owns the fixture?", "Example Team"
            table.cell(3, 0).text = "What is missing?"
            doc.save(source)
            parsed = parse_to_markdown(source)
            result = chunk_markdown(parsed.markdown, source.name, metadata=parsed.metadata)
            self.assertEqual(len(result.chunks), 1)
            row = result.chunks[0]
            self.assertFalse(row.review_required)
            self.assertEqual(row.table_row_number, 3)
            self.assertEqual(row.category, "General")
            self.assertTrue(any(w.code == "faq_answer_missing" for w in result.warnings))

    def test_actual_question_merge_stays_reviewed(self):
        with TemporaryDirectory() as temp:
            source = Path(temp) / "synthetic.docx"
            doc = Document()
            table = doc.add_table(rows=2, cols=2)
            table.cell(0, 0).text, table.cell(0, 1).text = "Question", "Answer"
            table.cell(1, 0).merge(table.cell(1, 1)).text = "Ambiguous question?"
            doc.save(source)
            parsed = parse_to_markdown(source)
            self.assertIn("review-required", parsed.markdown)
