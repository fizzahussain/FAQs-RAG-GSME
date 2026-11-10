"""Bounded Office extraction. Spreadsheet formulas are evidence, not calculations.

Never evaluate macros, external workbook links or formula expressions. Native
charts/images stay review evidence; no visual associations are inferred.
"""

from datetime import date, datetime, time
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile
from lxml import etree

from gsme_rag.models import EvidenceDiagnostic, ParsedDocument, ParseWarning
from gsme_rag.parsing.fidelity import _reason as fidelity_reason
from gsme_rag.parsing.links import markdown_link
from gsme_rag.parsing.markdown import finish_markdown, render_markdown_table


def check_package(path):
    """Reject excessive decompression before constructing an Office object tree."""
    with ZipFile(path) as package:
        entries = package.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > 128 * 1024 * 1024:
            raise ValueError("Office package exceeds the bounded extraction limit; split the source")
        if any(e.flag_bits & 1 for e in entries):
            raise ValueError("Encrypted Office packages are not supported")


def review(text, reason):
    return f"<!-- review-required: {reason} -->\n\n{text}\n\n<!-- review-end -->"


def diagnostic(kind, location, text, reason, page=None, bbox=None, alternate=None):
    identity = sha256(f"{kind}|{location}|{text}".encode()).hexdigest()[:20]
    return EvidenceDiagnostic(identity, kind, page, bbox, text, alternate, reason)


def parse_xlsx_to_markdown(path, source_name=None):
    from openpyxl import load_workbook
    check_package(path)
    parts, warnings, evidence = [], [], []
    book = load_workbook(path, data_only=False, keep_links=False)
    try:
        if len(book.worksheets) > 100 or sum(s.max_row * s.max_column for s in book.worksheets) > 500000:
            raise ValueError("Workbook exceeds 100 sheets / 500,000 cell positions; split the source")
        for sheet in book.worksheets:
            if sheet.sheet_state != "visible":
                warnings.append(ParseWarning("xlsx_hidden_sheet", "Hidden sheet retained only in the original, not retrieval", f"sheet {sheet.title}"))
                continue
            parts.append(f"# Worksheet: {sheet.title}")
            nonempty = [row for row in sheet.iter_rows() if any(c.value is not None for c in row)]
            if not nonempty:
                continue
            width = max(c.column for row in nonempty for c in row if c.value is not None)
            if width > 512:
                raise ValueError("Worksheet exceeds 512 populated columns; split the source")
            first = nonempty[0]
            headers = [str(c.value) if c.value is not None else f"Column {c.column_letter}" for c in first[:width]]
            faq = {h.strip().casefold() for h in headers} >= {"question", "answer"}
            if not faq:
                headers = [f"Column {c.column_letter}" for c in first[:width]]
                warnings.append(ParseWarning("xlsx_header_unverified", "No explicit Question/Answer header; all rows retained under physical column labels for review", f"sheet {sheet.title}"))
            header_uncertain = (len(set(headers)) != len(headers) or any(c.value is None for c in first[:width])
                                or any(c.coordinate in region for c in first[:width] for region in sheet.merged_cells.ranges))
            merged = list(sheet.merged_cells.ranges)
            # Each row is its own table so blank physical rows and review state
            # cannot shift the next row's source attribution.
            for row in nonempty[1:] if faq else nonempty:
                number = row[0].row
                values, suspect = [], not faq or header_uncertain
                for cell in row[:width]:
                    value = cell.value
                    text = "" if value is None else value.isoformat() if isinstance(value, (date, datetime, time)) else str(value)
                    reasons = []
                    if cell.data_type == "f":
                        reasons.append("Formula expression retained unevaluated; cached results are not trusted")
                    if cell.data_type == "e":
                        reasons.append("Workbook contains an error cell")
                    if cell.number_format != "General" and isinstance(value, (int, float)):
                        reasons.append(f"Stored numeric value; display number format is {cell.number_format!r}")
                    if fidelity_reason(text):
                        reasons.append(fidelity_reason(text))
                    if cell.hyperlink:
                        target = cell.hyperlink.target
                        if target and not reasons:
                            text = markdown_link(text, target)
                        else:
                            reasons.append("Hyperlink requires source inspection")
                    if any(cell.coordinate in region for region in merged):
                        reasons.append("Merged cell: blank neighbors were not filled by inference")
                    if reasons:
                        suspect = True
                        evidence.append(diagnostic("xlsx_cell", f"{sheet.title}!{cell.coordinate}", text,
                            f"{sheet.title}!{cell.coordinate}: " + "; ".join(reasons)))
                    values.append(text)
                # Hidden rows/columns may intentionally contain private working
                # data. Omit them from retrieval but leave bytes in the original.
                if sheet.row_dimensions[number].hidden or any(sheet.column_dimensions[c.column_letter].hidden for c in row[:width]):
                    warnings.append(ParseWarning("xlsx_hidden_cells", "Row with hidden cells omitted from retrieval", f"sheet {sheet.title}; row {number}"))
                    continue
                table = render_markdown_table([headers, values])
                parts.append(f"<!-- table: 1; first_data_row: {number} -->")
                parts.append(review(table, "spreadsheet-cell") if suspect else table)
            for index, chart in enumerate(sheet._charts):
                # Keep chart package evidence, not a guessed visual description.
                xml = etree.tostring(chart._write(), encoding="unicode")
                evidence.append(diagnostic("xlsx_chart", f"{sheet.title}; chart {index + 1}", xml,
                    f"Worksheet {sheet.title}, chart {index + 1}: original chart XML; visual mappings unverified"))
                warnings.append(ParseWarning("xlsx_chart_review", "Chart requires source review; no generated mapping was indexed", f"sheet {sheet.title}; chart {index + 1}"))
    finally:
        book.close()
    return ParsedDocument(source_name or Path(path).name, "xlsx", "openpyxl_cells_to_markdown",
                          finish_markdown(parts), warnings, evidence_diagnostics=evidence)


def parse_pptx_to_markdown(path, source_name=None):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
    check_package(path)
    deck = Presentation(path)
    if len(deck.slides) > 1000:
        raise ValueError("Presentation exceeds 1,000 slides; split the source")
    parts, warnings, evidence = [], [], []

    def shapes(items):
        for shape in items:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from shapes(shape.shapes)
            else:
                yield shape

    for number, slide in enumerate(deck.slides, 1):
        parts.extend([f"<!-- page: {number} -->", "<!-- section-break -->"])
        title = slide.shapes.title
        if title is not None and title.text.strip():
            parts.append("# " + title.text.replace("\n", " ").strip())
        for shape in shapes(slide.shapes):
            if shape == title:
                continue
            location = f"page {number}; shape {shape.shape_id}"
            bbox = tuple(float(v) for v in (shape.left, shape.top, shape.left + shape.width, shape.top + shape.height))
            if shape.element.xpath('.//*[local-name()="oMath"]'):
                evidence.append(diagnostic("pptx_formula", location, shape.element.xml,
                    "Formula source XML retained; symbols/layout require source review; bbox units EMU", number, bbox))
                warnings.append(ParseWarning("pptx_formula_review", "Formula is unavailable as trusted text", location))
                continue
            if shape.is_placeholder and shape.placeholder_format.type in {PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.SLIDE_NUMBER}:
                evidence.append(diagnostic("slide_furniture", location, shape.text if shape.has_text_frame else "",
                    "Explicit footer/date/page-number placeholder; excluded from retrieval", number, bbox))
                continue
            if shape.has_table:
                table = shape.table
                rows = [[c.text for c in row.cells] for row in table.rows]
                explicit_faq = bool(rows) and {c.strip().casefold() for c in rows[0]} >= {"question", "answer"}
                uncertain = not explicit_faq or any(c.is_merge_origin or c.is_spanned for row in table.rows for c in row.cells)
                rendered = render_markdown_table(rows)
                parts.append(review(rendered, "slide-table") if uncertain else rendered)
                if uncertain:
                    warnings.append(ParseWarning("pptx_merged_table", "Merged table cells require source review", location))
            elif shape.has_chart:
                # Preserve actual chart cache/labels as raw evidence. The chart
                # may have custom formatting or labels not represented here.
                evidence.append(diagnostic("pptx_chart", location, shape.chart._chartSpace.xml,
                    "Original chart XML including available labels/values; visual mapping unverified; bbox units EMU", number, bbox))
                warnings.append(ParseWarning("pptx_chart_review", "Chart retained as raw review evidence, not trusted claims", location))
            elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                evidence.append(diagnostic("pptx_image", location, "sha256:" + sha256(shape.image.blob).hexdigest(),
                    "Image remains in original PPTX; OCR/visual interpretation not verified; bbox units EMU", number, bbox))
                warnings.append(ParseWarning("pptx_image_review", "Embedded image is not described or verified automatically", location))
            elif shape.has_text_frame:
                lines = []
                for paragraph in shape.text_frame.paragraphs:
                    runs = [markdown_link(run.text, run.hyperlink.address) if run.hyperlink.address else run.text for run in paragraph.runs]
                    text = "".join(runs).strip()
                    if text:
                        lines.append(("  " * paragraph.level + "- " if paragraph.level else "") + text)
                text = "\n\n".join(lines)
                reason = fidelity_reason(text)
                parts.append(review(text, "text-fidelity") if reason else text)
                if reason:
                    evidence.append(diagnostic("pptx_text", location, text, reason + "; bbox units EMU", number, bbox))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            notes = slide.notes_slide.notes_text_frame.text
            evidence.append(diagnostic("pptx_notes", f"page {number}; notes", notes,
                "Speaker notes preserved separately; not visible slide evidence", number))
    return ParsedDocument(source_name or Path(path).name, "pptx", "python_pptx_shapes_to_markdown",
                          finish_markdown(parts), warnings, evidence_diagnostics=evidence)
