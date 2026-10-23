from collections.abc import Iterator
import hashlib
from pathlib import Path
import re

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from gsme_rag.parsing.markdown import finish_markdown, render_markdown_table
from gsme_rag.parsing.ocr import OcrUnavailable, ocr_image
from gsme_rag.models import ParseWarning, ParsedDocument
from gsme_rag.text import normalize_inline, normalize_multiline


def _iter_blocks(document: DocumentObject) -> Iterator[Paragraph | Table]:
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _format_runs(paragraph: Paragraph) -> str:
    groups: list[tuple[str, bool, bool]] = []
    for run in paragraph.runs:
        text = run.text.replace("\r", "").replace("\n", " ")
        if not text:
            continue
        bold, italic = run.bold is True, run.italic is True
        if groups and groups[-1][1:] == (bold, italic):
            previous, _, _ = groups[-1]
            groups[-1] = (previous + text, bold, italic)
        else:
            groups.append((text, bold, italic))

    fragments = []
    for text, bold, italic in groups:
        marker = "***" if bold and italic else "**" if bold else "*" if italic else ""
        fragments.append(f"{marker}{text}{marker}" if marker else text)
    rendered = "".join(fragments)
    return re.sub(r"[ \t]+", " ", rendered).strip()


def _paragraph_to_markdown(paragraph: Paragraph) -> str:
    text = _format_runs(paragraph)
    if not text:
        return ""

    style = paragraph.style.name if paragraph.style else "Normal"
    heading_match = re.match(r"Heading\s+([1-6])", style, flags=re.IGNORECASE)
    if heading_match:
        level = int(heading_match.group(1))
        return f"{'#' * level} {normalize_inline(paragraph.text)}"

    if style.lower().startswith("title"):
        return f"# {normalize_inline(paragraph.text)}"
    if "list bullet" in style.lower():
        return f"- {text}"
    if "list number" in style.lower():
        return f"1. {text}"
    return text


def _image_ids(element) -> Iterator[str]:
    for node in element.iter():
        if node.tag == qn("a:blip"):
            value = node.get(qn("r:embed"))
        elif node.tag == "{urn:schemas-microsoft-com:vml}imagedata":
            value = node.get(qn("r:id"))
        else:
            continue
        if value:
            yield value


def _image_text(element, part, location: str, warnings: list[ParseWarning],
                cache: dict[str, str | ValueError]) -> list[str]:
    output: list[str] = []
    for relationship_id in _image_ids(element):
        try:
            image = part.related_parts[relationship_id]
            if not image.content_type.startswith("image/"):
                raise ValueError("Embedded object is not an image")
            data = image.blob
            digest = hashlib.sha256(data).hexdigest()
            if digest not in cache:
                try:
                    cache[digest] = ocr_image(data)
                except ValueError as error:
                    cache[digest] = error
            recognized = cache[digest]
            if isinstance(recognized, ValueError):
                raise recognized
            if recognized:
                output.append(recognized)
                warnings.append(ParseWarning(
                    code="ocr_needs_review",
                    message="Text read from an embedded Word image needs source review, especially values and table cells",
                    location=location,
                ))
            else:
                warnings.append(ParseWarning(
                    code="image_no_text", message="No text was recovered from this image; a chart or diagram may still contain information",
                    location=location,
                ))
        except (KeyError, OcrUnavailable, ValueError) as error:
            warnings.append(ParseWarning(
                code="image_ocr_unavailable",
                message=f"Embedded image could not be read: {error}", location=location,
            ))
    return output


def _cell_text(cell, warnings: list[ParseWarning], location: str,
               cache: dict[str, str | ValueError]) -> tuple[str, bool]:
    parts: list[str] = []
    has_ocr = False
    for paragraph in cell.paragraphs:
        value = normalize_inline(paragraph.text)
        recognized = _image_text(paragraph._p, paragraph.part, location, warnings, cache)
        if recognized:
            has_ocr = True
            value = "\n".join([value, *recognized]).strip()
        if value:
            parts.append(value)
    return normalize_multiline("\n".join(parts)), has_ocr


def parse_docx_to_markdown(
    path: str | Path,
    source_name: str | None = None,
) -> ParsedDocument:
    input_path = Path(path)
    display_name = source_name or input_path.name
    document = Document(input_path)
    parts: list[str] = []
    warnings: list[ParseWarning] = []
    table_number = 0
    image_cache: dict[str, str | ValueError] = {}
    paragraph_number = 0

    for block in _iter_blocks(document):
        if isinstance(block, Paragraph):
            paragraph_number += 1
            rendered = _paragraph_to_markdown(block)
            if rendered:
                parts.append(rendered)
            for recognized in _image_text(block._p, block.part, f"paragraph {paragraph_number}", warnings, image_cache):
                parts.extend(("<!-- review-required: docx-image-ocr -->", recognized, "<!-- review-end -->"))
            continue

        table_number += 1
        table_has_ocr = False
        rows = []
        for row_number, row in enumerate(block.rows, start=1):
            cells = []
            for column_number, cell in enumerate(row.cells, start=1):
                value, has_ocr = _cell_text(cell, warnings, f"table {table_number} row {row_number} column {column_number}", image_cache)
                cells.append(value)
                table_has_ocr |= has_ocr
            rows.append(cells)
        if not rows:
            warnings.append(
                ParseWarning(
                    code="empty_table",
                    message="An empty Word table was ignored",
                    location=f"table {table_number}",
                )
            )
            continue
        if any(cell.tables for row in block.rows for cell in row.cells):
            warnings.append(
                ParseWarning(
                    code="nested_table_review",
                    message="Nested tables need manual review; cell paragraph text was kept",
                    location=f"table {table_number}",
                )
            )
        first_data_row = 2
        if len(rows[0]) == 2 and rows[0][0].rstrip().endswith("?") and rows[0][1]:
            rows.insert(0, ["Question", "Answer"])
            first_data_row = 1
            warnings.append(
                ParseWarning(
                    code="table_header_inferred",
                    message="A two-column question/answer table had no visible header",
                    location=f"table {table_number}",
                )
            )
        table_markdown = (
            f"<!-- table: {table_number}; first_data_row: {first_data_row} -->\n"
            + render_markdown_table(rows)
        )
        if table_has_ocr:
            parts.append("<!-- review-required: docx-image-ocr -->")
        parts.append(table_markdown)
        if table_has_ocr:
            parts.append("<!-- review-end -->")

    # Word headers/footers can contain embedded diagrams too; visit each part once.
    visited = set()
    for section in document.sections:
        for label, region in (("header", section.header), ("footer", section.footer)):
            part = region.part
            if part.partname in visited:
                continue
            visited.add(part.partname)
            for recognized in _image_text(part.element, part, label, warnings, image_cache):
                parts.extend(("<!-- section-break -->", "<!-- review-required: docx-image-ocr -->",
                              f"{label.title()} image text (OCR): {recognized}", "<!-- review-end -->"))

    markdown = finish_markdown(parts)
    if "\ufffd" in markdown:
        warnings.append(ParseWarning(
            code="source_replacement_character",
            message=(
                "The DOCX contains Unicode replacement characters; the missing original "
                "characters cannot be reconstructed safely"
            ),
        ))
    if not markdown:
        warnings.append(
            ParseWarning(
                code="no_document_content",
                message="No readable paragraphs or tables were found in the DOCX file",
            )
        )

    return ParsedDocument(
        filename=display_name,
        document_type="docx",
        strategy_used="docx_structure_to_markdown",
        markdown=markdown,
        warnings=warnings,
    )
