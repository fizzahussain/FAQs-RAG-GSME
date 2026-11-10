"""Office fixtures are generated locally; no private source is needed."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from gsme_rag.parsing import parse_to_markdown
from gsme_rag.chunking import chunk_markdown


class OfficeTests(TestCase):
    def test_xlsx_faq_links_physical_rows_formula_and_merged_cells(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "faq.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "FAQs"
            sheet.append(["Question", "Answer"])
            sheet.append(["Where is the synthetic site?", "Original link"])
            sheet["B2"].hyperlink = "https://example.com/original"
            sheet.append([None, None])
            sheet.append(["What is the synthetic value?", "=SUM(1,2)"])
            sheet.append(["Who owns the synthetic process?", "Example Team"])
            sheet.merge_cells("B5:C5")
            book.save(path)
            parsed = parse_to_markdown(path)
            chunks = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata).chunks
            self.assertEqual(parsed.strategy_used, "openpyxl_cells_to_markdown")
            self.assertIn("https://example.com/original", parsed.markdown)
            # A truly blank column extends the worksheet's grid but is not a
            # license to invent a header; such rows stay under review.
            formula = next(c for c in chunks if "SUM" in c.text)
            self.assertEqual(formula.table_row_number, 4)
            self.assertTrue(formula.review_required)
            merged = next(c for c in chunks if "Example Team" in c.text)
            self.assertEqual(merged.table_row_number, 5)
            self.assertTrue(merged.review_required)
            self.assertTrue(any("FAQs!B4" in e.reason for e in parsed.evidence_diagnostics))

    def test_clean_xlsx_faq_is_eligible_and_sheet_context_survives(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "clean.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "Example FAQ"
            sheet.append(["Question", "Answer"])
            sheet.append(["Who owns the synthetic process?", "Example Team"])
            book.save(path)
            parsed = parse_to_markdown(path)
            chunks = chunk_markdown(parsed.markdown, path.name, metadata=parsed.metadata).chunks
            row = next(c for c in chunks if "Example Team" in c.text)
            self.assertFalse(row.review_required)
            self.assertEqual(row.table_row_number, 2)
            self.assertIn("Worksheet: Example FAQ", row.heading_path)
            self.assertIsNone(row.source_page_start)

    def test_generic_xlsx_keeps_first_row_hidden_cells_and_chart_evidence(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "generic.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.append(["Category", "Value"])
            sheet.append(["A", 20])
            sheet.append(["B", 80])
            sheet.append(["PRIVATE HIDDEN", 99])
            sheet.row_dimensions[4].hidden = True
            chart = BarChart()
            chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=3), titles_from_data=True)
            sheet.add_chart(chart, "D1")
            book.save(path)
            parsed = parse_to_markdown(path)
            self.assertIn("Category", parsed.markdown)
            self.assertIn("20", parsed.markdown)
            self.assertNotIn("PRIVATE HIDDEN", parsed.markdown)
            self.assertTrue(any(e.kind == "xlsx_chart" for e in parsed.evidence_diagnostics))
            self.assertTrue(all(c.review_required for c in chunk_markdown(parsed.markdown).chunks))

    def test_pptx_slide_pages_hyperlink_chart_and_notes(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "slides.pptx"
            deck = Presentation()
            slide = deck.slides.add_slide(deck.slide_layouts[5])
            slide.shapes.title.text = "Synthetic overview"
            frame = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(5), Inches(1)).text_frame
            run = frame.paragraphs[0].add_run()
            run.text = "Original link"
            run.hyperlink.address = "https://example.com/original"
            slide.notes_slide.notes_text_frame.text = "Internal speaker notes, not visible evidence"
            slide = deck.slides.add_slide(deck.slide_layouts[5])
            slide.shapes.title.text = "Chart example"
            data = CategoryChartData()
            data.categories = ["A", "B"]
            data.add_series("Example", (20, 80))
            slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(4), Inches(3), data)
            deck.save(path)
            parsed = parse_to_markdown(path)
            self.assertIn("https://example.com/original", parsed.markdown)
            self.assertNotIn("Internal speaker notes", parsed.markdown)
            chart = next(e for e in parsed.evidence_diagnostics if e.kind == "pptx_chart")
            self.assertEqual(chart.page, 2)
            self.assertIn("80", chart.original_text)
            self.assertIn("<c:v>A</c:v>", chart.original_text)
            self.assertTrue(any(e.kind == "pptx_notes" for e in parsed.evidence_diagnostics))
            for chunk in chunk_markdown(parsed.markdown, path.name).chunks:
                if "Chart example" in chunk.text:
                    self.assertEqual(chunk.source_page_start, 2)

    def test_workbook_limits_reject_instead_of_truncating(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sparse.xlsx"
            book = Workbook()
            book.active["XFD1048576"] = "Far away value"
            book.save(path)
            with self.assertRaisesRegex(ValueError, "500,000"):
                parse_to_markdown(path)
