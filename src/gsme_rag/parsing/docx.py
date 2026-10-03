from collections.abc import Iterator
from pathlib import Path
import re

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from gsme_rag.parsing.markdown import finish_markdown, render_markdown_table
from gsme_rag.models import ParseWarning, ParsedDocument
from gsme_rag.text import normalize_inline, normalize_multiline


def _iter_blocks(document: DocumentObject) -> Iterator[Paragraph | Table]:
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _format_runs(paragraph: Paragraph) -> str:
    fragments: list[str] = []
    for run in paragraph.runs:
        text = run.text.replace("\r", "").replace("\n", " ")
        if not text:
            continue
        if run.bold is True and run.italic is True:
            fragments.append(f"***{text}***")
        elif run.bold is True:
            fragments.append(f"**{text}**")
        elif run.italic is True:
            fragments.append(f"*{text}*")
        else:
            fragments.append(text)

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


def _cell_text(cell) -> str:
    parts = [normalize_inline(paragraph.text) for paragraph in cell.paragraphs]
    return normalize_multiline("\n".join(part for part in parts if part))


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

    for block in _iter_blocks(document):
        if isinstance(block, Paragraph):
            rendered = _paragraph_to_markdown(block)
            if rendered:
                parts.append(rendered)
            continue

        table_number += 1
        rows = [[_cell_text(cell) for cell in row.cells] for row in block.rows]
        if not rows:
            warnings.append(
                ParseWarning(
                    code="empty_table",
                    message="An empty Word table was ignored",
                    location=f"table {table_number}",
                )
            )
            continue
        parts.append(f"<!-- table: {table_number} -->\n{render_markdown_table(rows)}")

    markdown = finish_markdown(parts)
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
